# Options — Implementation Truth Document

> **Single source of truth** for the options feature in learn-ai —
> the surviving surfaces (`/strategy-builder`, `/pricing-lab`, the
> options sub-feature of `/data-lab`), the math they invoke, the data
> flow, and how we know it's correct.
>
> Modeled on `docs/architecture/iv-ownership-research.md`. Re-revise
> this doc on subsequent reviews; do not spawn sibling docs.
>
> **Status:** *MVP scaffold — Phase 5 of the cleanup is in progress.*
> The §1–§3 framing and §4 math index are populated; per-page §5.x
> sections are stubs awaiting the R0b / R1 migrations to land. Once
> those migrations ship, each §5.x is fleshed out with the production
> pipeline, end-to-end fixture cite, and worked numerical example
> per the rigor template in
> `docs/architecture/options-routes-research.md` § 6.2.
>
> **Last revised:** 2026-04-29 (initial scaffold).

---

## Table of contents

1. [Reviewer framing](#1-reviewer-framing)
2. [Executive overview](#2-executive-overview)
3. [Hard constraints](#3-hard-constraints)
4. [Mathematical foundations](#4-mathematical-foundations)
5. [Production pipelines](#5-production-pipelines)
6. [Tolerances and validation](#6-tolerances-and-validation)
7. [Decisions log](#7-decisions-log)
8. [Reviewer feedback log](#8-reviewer-feedback-log)
9. [Future plan / deferred items](#9-future-plan--deferred-items)
10. [Out of scope](#10-out-of-scope)
11. [References + PR audit trail](#11-references--pr-audit-trail)
12. [Appendix A — worked numerical examples](#12-appendix-a--worked-numerical-examples)
13. [Appendix B — file map](#13-appendix-b--file-map)

---

## 1. Reviewer framing

This document is a self-contained brief for both internal readers
(future-self, contributors) and external LLM reviewers asked for a
second-opinion quant review of the options-feature implementation.
It is the per-feature analogue of `iv-ownership-research.md` and uses
the same conventions:

- §4 (Mathematical foundations) cites every formula with its canonical
  source, implementation file:line, tolerance level, and a worked
  textbook example reproducible without vendor access.
- §5 (Production pipelines) walks the data flow per surviving page,
  with every wire-timestamp annotated `int64 ms UTC` per the canonical
  format rule from `numerical-rigor.md`.
- §6 (Tolerances and validation) cites a passing test for every
  numerical claim made in §4 and §5.
- §7 (Decisions log) is the audit trail for design choices; §8 is the
  external-reviewer-feedback log.

**Style preferences for any reviewer response:**

- **Specificity > breadth.** Quote the specific section above when
  flagging something. "§4.2 third bullet" rather than "your Greek
  formula".
- **Cite published sources** when rebutting, with section/page numbers.
- **Don't pull punches.** Direct corrections are more useful than
  hedged suggestions.

---

## 2. Executive overview

### 2.1 What the options feature does

Three surviving surfaces after the 2026-04-29 cleanup
(`docs/architecture/options-routes-research.md` § 5.1):

- **`/strategy-builder`** — chain viewer + multi-leg payoff builder.
  Single page where the user picks an underlying, picks an expiration,
  reads the live chain, builds a strategy (1–N legs from templates or
  manual entry), and analyzes its payoff curve, Greeks, POP (probability
  of profit), expected value, and breakevens. Optional what-if scenarios
  (T+5d, IV ±10%) and a QuantLib-engine pricing toggle.
- **`/pricing-lab`** — single-contract multi-engine pricing comparison.
  Same chain-fetch prelude as strategy-builder, then `/api/quantlib/compare`
  produces price + Greek curves over a spot grid for several pricing
  engines (Legacy A&S BS, Python BS, QuantLib analytic BS, binomial
  CRR/JR/LR, finite-diff, Monte Carlo). The point of the page is
  cross-engine validation — "do the numbers agree where they should?"
- **`/data-lab` options sub-feature** — companion-data configuration
  for backtesting research (today: 4 knobs that attach an
  `options_companion` block to a dataset run). After R1 lands, this
  page also hosts a past-chain inspector ported from the deleted
  `/options-history` route, giving an interactive preview of what
  the bundle will fetch.

Plus one *deleted* route that lives on as a redirect during the
7-day watch period: `/options-strategy-lab` → `/strategy-builder`.

### 2.2 What is owned vs delegated

**Owned (single source of truth in this codebase):**

- Black-Scholes price + Greeks (closed-form, sub-day-resolution
  safe) — `app/services/bs_greeks.py`.
- Implied volatility solver (QuantLib primary, scipy `brentq`
  fallback, intraday `min_ttm` support) — `app/volatility/solver.py`.
- POP / expected value (Black-Scholes lognormal model) —
  `app/services/strategy_engine.py`.
- Multi-engine pricing comparison — `app/services/quantlib_pricer.py`
  (analytic BS, binomial CRR/JR/LR, finite-diff, Monte Carlo).
- Skew metrics, put-call parity forward — `app/volatility/analytics.py`.

**Delegated (vendor or upstream, treated as data only):**

- Polygon.io chain snapshots, contract metadata, Greeks-as-published.
  We render the published Greeks but trust our own `bs_greeks` Greeks
  for any computation. Polygon's `implied_volatility` field is *not*
  trusted as authoritative — see `iv-ownership-research.md` §2.
- FRED `DGS1MO` for the risk-free rate (per-tenor interpolation).

### 2.3 Headline empirical anchor

> *To be populated when R0b ships and §5.1 has its end-to-end fixture.*
> Anchor will be SPY 2024-12-20 per [§7 D6](#7-decisions-log) of the
> research plan: a textbook BS price example for §4 math (Hull, §15.8,
> p. 332), and a real-market `analyzeOptionsStrategy` round-trip for
> §5.1 pipeline.

---

## 3. Hard constraints

These are inherited from the platform (`CLAUDE.md` and
`numerical-rigor.md`) and apply to every options surface.

| Constraint | What it rules out |
|---|---|
| **Polygon Starter plan** (2y history, 15-min delayed, no historical bid/ask) | Real backtested historical bid/ask. Forced spread synthesis: `bid = max($0.05, 0.5%·close)`, `ask = close + half_spread`. Snapshot endpoint is *live-only*; reconstructing past chains requires per-contract aggregate scans (the `data-lab` past-chain inspector path per §5.3). |
| **`int64 ms UTC` at all wire/storage boundaries** | `DateTime`, `datetime`, ISO-string-with-`Z` are banned. Two and only two conversion boundaries: ingestion (parse-to-int) and UI rendering (int-to-display-string). |
| **`America/New_York` for wall-clock semantics, never persisted** | Session filters and exchange-aligned bar starts are ET; conversion is per-operation, never written to disk. |
| **No silent forward-fill / synthetic alignment** | Sparse and dense chain rows must not be patched. Missing strikes are signals, not noise. |
| **Single source of truth per concept** (CLAUDE.md §5) | Greek calculations, IV solving, BS pricing all live in exactly one Python module. The frontend renders; the .NET resolvers proxy. |
| **Sovereignty over the math** | Vendor IV / Greeks fields are stored as diagnostics only. We re-solve. |

Page-specific constraints:

- **`/data-lab` past-chain inspector** (§5.3) — Polygon Starter's
  snapshot endpoint is live-only, so historical chains are
  reconstructed by batched per-contract aggregate scans (30 contracts
  at a time). Documented in §5.3.

---

## 4. Mathematical foundations

Per [§7 D6](../architecture/options-routes-research.md#7-decisions-log)
of the research plan, every formula is anchored on a textbook example
(Hull, *Options, Futures, and Other Derivatives*, 9th ed.) so that
external reviewers can independently verify without vendor access.
End-to-end production fixtures (§5.x) anchor on real market dates.

The canonical authority for every formula in this section is
[`docs/architecture/options-math-authorities.md`](options-math-authorities.md).

### 4.1 Black-Scholes European price

**Equation** (Hull §15.8, p. 332; q = continuous dividend yield):

$$C = S e^{-qT} N(d_1) - K e^{-rT} N(d_2)$$
$$P = K e^{-rT} N(-d_2) - S e^{-qT} N(-d_1)$$
$$d_1 = \frac{\ln(S/K) + (r - q + \sigma^2/2) T}{\sigma \sqrt{T}}, \quad d_2 = d_1 - \sigma \sqrt{T}$$

**Canonical source.** Hull §15.8, p. 332 (closed form);
[`PythonDataService/app/services/bs_greeks.py:110`](../../PythonDataService/app/services/bs_greeks.py#L110)
implements `bs_european_price(S, K, T, r, sigma, q=0.0, option_type='call')`.

**Tolerance level.** Strict float — `atol=1e-9, rtol=0` per
[`numerical-rigor.md` § Default tolerances](../../.claude/rules/numerical-rigor.md).

**Worked textbook example.** Hull §15.9, Example 15.6: `S=42, K=40, r=0.10,
σ=0.20, T=0.5` →

- `d1 = 0.7693, d2 = 0.6278`
- `N(d1) = 0.7791, N(d2) = 0.7349`
- `C = 4.7594` (call), `P = 0.8086` (put)

**Implementation note.** `bs_greeks.bs_european_price` accepts an
optional continuous dividend yield `q` (default 0); when computed from
real chain data, `q` is the put-call-parity-implied yield from §4.4.

### 4.2 Black-Scholes Greeks

**Equations** (Hull §17.6–17.10, pp. 380–390). Greeks for the call;
put Greeks follow by the parity relationships.

| Greek | Formula |
|---|---|
| Delta (Δ) | `e^{-qT} · N(d1)` |
| Gamma (Γ) | `e^{-qT} · N'(d1) / (S σ √T)` |
| Theta (Θ) | `-S e^{-qT} N'(d1) σ / (2√T) − r K e^{-rT} N(d2) + q S e^{-qT} N(d1)` |
| Vega (ν) | `S e^{-qT} √T · N'(d1)` (per 1.0 vol unit; UI scales by /100 for per-1% display) |
| Rho (ρ) | `K T e^{-rT} N(d2)` |

**Canonical source.** Hull §17.6–17.10;
[`PythonDataService/app/services/bs_greeks.py:48`](../../PythonDataService/app/services/bs_greeks.py#L48)
implements `black_scholes_greeks(S, K, T, r, sigma, q, option_type)`
returning a `GreeksResult` dataclass.

**Tolerance level.** Strict float for analytical Greeks
(`atol=1e-9, rtol=0`); the QuantLib numerical-bump path used in
`/pricing-lab`'s comparison curves uses `atol=1e-6, rtol=1e-6` per
[`numerical-rigor.md`](../../.claude/rules/numerical-rigor.md).

**Worked textbook example.** Same anchor as §4.1
(`S=42, K=40, r=0.10, σ=0.20, T=0.5`) →

- Delta = 0.7791
- Gamma = 0.0497
- Theta (per year) = −4.301
- Vega (per 1.0 vol) = 8.81
- Rho = 13.99

### 4.3 Implied volatility solver

**Goal.** Given a market price `P_mkt`, solve for σ such that
`bs_european_price(S, K, T, r, σ, q) = P_mkt`. Closed-form inverse
does not exist; the solver uses a fallback chain.

**Fallback chain** (per
[`PythonDataService/app/volatility/solver.py:109`](../../PythonDataService/app/volatility/solver.py#L109)):

1. **QuantLib `VanillaOption.impliedVolatility()`** — the primary path;
   robust internal Newton-Raphson with QuantLib's full pricing engine.
2. **scipy `brentq` over `[1e-6, 5.0]`** — fallback for QL convergence
   failures or sub-day TTMs that QL's date-based engine collapses to
   zero.

The result is wrapped in `ImpliedVolResult` with diagnostics: which
branch converged, the iteration count, the bracket, and the residual.

**Intraday safety.** The solver accepts a `min_ttm` parameter (default
`1.0 / (252 × 78)` ≈ 5 minutes). For TTMs smaller than `min_ttm`, the
function returns `NaN` rather than producing a junk number from a
collapsed-time engine. This is the load-bearing reason callers like
the IV recorder pass an explicit `min_ttm`.

**Tolerance level.** `atol=1e-6` on the residual `|P_solver − P_mkt|`
per the IV-doc convention.

**Bounds rationale.** σ ∈ [1e-6, 5.0] covers everything observed in
practice; the lower bound prevents Newton-Raphson divisions by zero,
the upper bound is a sanity gate.

### 4.4 Forward price + dividend yield from put-call parity

**Equation.** From put-call parity on a European chain at strike K
with TTM T:

$$F = K + (C - P) \cdot e^{rT}$$
$$q = r - \frac{\ln(F / S)}{T}$$

**Canonical source.** Hull §10.4 (parity); §17.13 (dividend implied
from parity).
[`PythonDataService/app/volatility/analytics.py:304`](../../PythonDataService/app/volatility/analytics.py#L304)
implements `compute_put_call_parity_forward(...)` returning the
implied forward `F` per TTM. The implied `q` is derived as
`r - ln(F/S)/T` at the call site; there is no separate function for
`q` (intentional — it's a one-line derivation).

**Tolerance level.** Strict float on `F`; the implied `q` inherits
the `r` and `F` tolerances.

### 4.5 Probability of profit (POP) for a strategy

**Equation.** Under the Black-Scholes lognormal model, the
probability that the strategy P&L at expiry is non-negative is:

$$\text{POP} = \int_{S_{\text{profitable}}} f_{\text{lognormal}}(S_T \mid S_0, r-q, \sigma, T) \, dS_T$$

where `S_{profitable}` is the union of intervals where
`compute_payoff_at_expiry(legs, S_T) ≥ 0`, and the lognormal density
uses scipy's `lognorm.pdf` with:

- `scale = S_0 · e^{(r-q)T}`
- `s = σ √T`

**Canonical source.** Hull §15.6 (lognormal property of stock prices);
[`PythonDataService/app/services/strategy_engine.py`](../../PythonDataService/app/services/strategy_engine.py)
(see `compute_pop`).

**Tolerance level.** `atol=1e-6` on the cumulative probability;
`rtol=1e-6` on the relative error. Numerical integration is via
scipy `lognorm.cdf` differences across the breakeven intervals, so
no quadrature error to worry about beyond the underlying CDF.

**Worked example anchor.** *(To be populated with the §5.1 worked
strategy example.)*

### 4.6 Multi-engine pricing comparison

For `/pricing-lab`, the same European option is priced under several
engines simultaneously to surface model-risk:

| Engine | Library | Convergence behaviour |
|---|---|---|
| Legacy BS (A&S) | TS [`black-scholes.ts`](../../Frontend/src/app/utils/black-scholes.ts) | Closed-form; \|ε\| < 1.5×10⁻⁷ on the normal CDF approximation. *(Slated for deletion in R8 once the server-side authority is the only path; see `options-vol-platform-tdd.md` Phase 1.2.)* |
| Python BS (scipy) | scipy `norm.cdf` | Closed-form; full double precision. |
| QuantLib analytic BS | QuantLib C++ via SWIG | Closed-form. |
| QuantLib binomial CRR | 801-step lattice | Up factor `u = e^{σ√Δt}`, `d = 1/u`. Converges to BS as steps → ∞. |
| QuantLib binomial JR | 801-step lattice (Jarrow-Rudd) | Equal-probability tree; reduces oscillation vs CRR. |
| QuantLib binomial LR | 801-step lattice (Leisen-Reimer) | Improved convergence rate near at-the-money. |
| Finite differences | QuantLib FDEuropeanEngine | Crank-Nicolson PDE solver. |
| Monte Carlo | QuantLib MC | High-variance — included for visual demonstration of MC noise. |

All engines are exposed via the unified
`POST /api/quantlib/compare` endpoint
([`PythonDataService/app/routers/quantlib_options.py`](../../PythonDataService/app/routers/quantlib_options.py))
and surfaced through the GraphQL `pricingModelComparison` resolver
([`Backend/GraphQL/Query.cs:1290`](../../Backend/GraphQL/Query.cs#L1290)).

Cross-engine tolerance: see §6 below for the parity bands the
`/pricing-lab` page enforces between any two engines that *should*
agree (e.g., "Python BS" vs "QuantLib analytic BS").

---

## 5. Production pipelines

### 5.1 `/strategy-builder` (chain view + payoff builder)

*Stub — to be authored after R0b ships.* Will follow the per-page
template in
`docs/architecture/options-routes-research.md` § 14 Appendix B.

Key skeleton points planned:

- Data flow: ticker → `getOptionsExpirations` (G1) →
  `getOptionsChainSnapshot` (G2) → render → user builds legs →
  `analyzeOptionsStrategy` (G3) → render payoff/Greek curves.
- Compute path: invokes §4.1 (BS price), §4.2 (Greeks), §4.5 (POP).
- After R0b: also hosts the per-contract historical drill-down
  drawer (D9 + D9a — full Greek display per row, drill-down on
  click). UX details deferred to the design-pass per
  [§7 D11](options-routes-research.md#7-decisions-log) of the
  research plan; entries UX-Q1, UX-Q2, UX-Q4 in
  [`options-ux-design-prompt.md`](options-ux-design-prompt.md).
- End-to-end fixture: SPY 2024-12-20 multi-leg strategy with
  golden POP + breakevens.

### 5.2 `/pricing-lab` (multi-engine pricing comparison)

*Stub — to be authored.* Skeleton:

- Data flow: ticker → expirations → chain → contract selection →
  `pricingModelComparison` (G4) over a spot grid → render
  multi-curve chart with optional diff-vs-reference overlay.
- Compute path: invokes §4.1, §4.2, §4.6.
- End-to-end fixture: a fixed (K, T, r, σ) tuple over a 100-point
  spot grid; assert cross-engine parity between Python BS and
  QuantLib analytic BS at `atol=1e-6` per §6.

### 5.3 `/data-lab` options sub-feature

*Stub — to be authored after R1 ships.* Skeleton:

- Existing surface: companion-config knobs that attach an
  `options_companion` block to a dataset run request.
- After R1: a past-chain inspector card on the options-companion
  config row (D10, D10a). The card lets the user preview what the
  bundle will fetch on a given past date — calls/puts split, ATM
  marker, change-from-prior-close, scan-results audit, per-contract
  drill-down.
- Live-vs-historical constraints box: the snapshot endpoint is live
  only on Polygon Starter. Past chains are reconstructed by batched
  per-contract aggregate scans (30 contracts per batch) of OCC
  tickers constructed via [`utils/occ-ticker.ts`](../../Frontend/src/app/utils/occ-ticker.ts).
- Data flow: ticker + past date → `past-chain.service.fetchPastChain(...)`
  → batched `getOrFetchStockAggregates(occTicker, ...)` → renders.
- UX details deferred to the design-pass per
  [§7 D11](options-routes-research.md#7-decisions-log); entry UX-Q3
  in [`options-ux-design-prompt.md`](options-ux-design-prompt.md).

### 5.4 Companion data formats

*Stub — to absorb [`docs/options-companion-format.md`](../options-companion-format.md)*
once Phase 5 authoring continues. The wire-format spec for the 30-day
IV companion series stays as currently documented; the absorb pass
just relocates it under §5.4 here and replaces the source file with
a redirect stub per § 6.1 of the research plan.

---

## 6. Tolerances and validation

Each cited assertion below has a passing test recorded by file:line.
The cite is what makes the doc auditable — without it, "we tested
this" is just a claim.

| Assertion | Tolerance | Test |
|---|---|---|
| `bs_european_price(S=42, K=40, r=0.10, σ=0.20, T=0.5) ≈ 4.7594` | atol=1e-4 (textbook 4-decimal) | `tests/services/test_bs_greeks.py` (existing) |
| Strategy POP for bull-call-spread, long-straddle, iron-condor | behavioural (in-range, monotone where expected) | `tests/test_strategy_engine.py` `TestPOP` (5 cases) |
| `analyzeOptionsStrategy` Phase 1.1 flag propagation (current curve, Greek curves, leg diagnostics) | shape-equality | `Backend.Tests/Unit/GraphQL/QueryTests.cs` `AnalyzeOptionsStrategy_Phase11Flags_PropagateToService` |
| `pricingModelComparison` returns one model curve per engine, points have all 6 fields (spot/price/Δ/Γ/Θ/ν/ρ) | shape-equality | `Backend.Tests/Unit/GraphQL/QueryTests.cs` `PricingModelComparison_Success_MapsModelCurves` |
| `getOptionsExpirations` filter pass-through | argument-equality | `Backend.Tests/Unit/GraphQL/QueryTests.cs` `GetOptionsExpirations_PassesFiltersThrough` |
| `/strategy-builder` analyze workflow forwards enabled-only legs | behavioural | `Frontend/src/app/components/strategy-builder/strategy-builder.component.spec.ts` `SB-C: forwards the enabled legs payload to the resolver` |
| `/strategy-builder` propagates resolver errors to UI | behavioural | `Frontend/src/app/components/strategy-builder/strategy-builder.component.spec.ts` `SB-C: propagates resolver-level errors` |
| `/pricing-lab` forwards `(spot, strike, σ, expiration, optionType, r, range, numPoints)` | argument-equality | `Frontend/src/app/components/pricing-lab/pricing-lab.component.spec.ts` `PL-B: forwards (spot, strike, vol, ...)` |
| `/pricing-lab` reports missing-IV when contract is incomplete | behavioural | `Frontend/src/app/components/pricing-lab/pricing-lab.component.spec.ts` `PL-E: reports the missing fields` |
| OCC ticker round-trip parity (`format(parse(t)) === t` for all SPY 2024-12-20 contracts) | bit-exact | `Frontend/src/app/utils/occ-ticker.spec.ts` `round-trip parity (R5 §8.2)` |

Additional cites will be added to this table as §5.x sections are
fleshed out (each will introduce its end-to-end fixture and the
cross-engine parity assertions).

---

## 7. Decisions log

The full decision history for the *cleanup* lives in
[`docs/architecture/options-routes-research.md` § 7](options-routes-research.md#7-decisions-log).
This section captures only decisions that bear on the *math or wire
shape* of the surviving feature.

| Date | Decision | Rationale | Reference |
|---|---|---|---|
| 2026-04-25 | `bs_solver.py` deleted; `bs_european_price` and `bs_european_vega` consolidated into `services/bs_greeks.py`. Two-IV-solver hazard eliminated. | Single source of truth per concept (CLAUDE.md §5). | [`options-math-authorities.md` § History](options-math-authorities.md) |
| 2026-04-29 | `quantlib_pricer.implied_volatility` clarified as *internal* to the QuantLib branch of `volatility/solver.implied_volatility`'s fallback chain. Direct callers must use the latter. | Doc-rot fix during Phase 1 of cleanup. | [`options-math-authorities.md` § Single source of truth](options-math-authorities.md) |
| 2026-04-29 | `analyzeOptionsStrategy` confirmed as a GraphQL **query** (not mutation). No state change; pure compute. | Resolver verification during Phase 1 of cleanup. | [`Backend/GraphQL/Query.cs:834`](../../Backend/GraphQL/Query.cs#L834) |
| 2026-04-29 | OCC ticker parsing/formatting consolidated into `Frontend/src/app/utils/occ-ticker.ts` with round-trip parity test. | R5 of the route cleanup. | [`Frontend/src/app/utils/occ-ticker.ts`](../../Frontend/src/app/utils/occ-ticker.ts) |

---

## 8. Reviewer feedback log

| Date | Reviewer | Feedback | Status | Resolution |
|---|---|---|---|---|
| *(empty — first reviewer pass scheduled for Phase 7 of the cleanup, after R0b and R1 ship)* | | | | |

---

## 9. Future plan / deferred items

Tracked items that affect the math or wire shape of the surviving
feature. *Cleanup-mechanical* items (extractions, redirects,
component deletions) live in
[`docs/architecture/options-routes-research.md` § 9](options-routes-research.md#9-phased-execution-plan).

- **R8 — server-side BS authority migration.** Phase 1.2 of
  `options-vol-platform-tdd.md`. Today, `/pricing-lab` and
  `/strategy-builder` both retain a TS-side `utils/black-scholes.ts`
  fallback. The endgame is the Python authority is the only path;
  the TS file is deleted with a parity test pinning the agreement at
  `atol=1e-9` on price and `atol=1e-6` on Greeks across a 1000-point
  spot grid. Deferred to a focused session because it touches
  sovereignty math.
- **R6 / R7 / R4 — post-consolidation extractions.** Greek
  formatters, contract-price-picker, options-chain-state-service.
  After R0a/R0b/R1 land, the duplication landscape shrinks; the
  remaining duplicates can be extracted with smaller blast radius.
- **0DTE intraday-IV slot wiring.** The IV recorder ships 4 daily
  slots already (see `iv-ownership-research.md` § 7.6); using the
  intraday slot to backfill a `/strategy-builder` "what was the IV
  at 09:35 ET" view is unbuilt.
- **POP under stochastic vol.** Today the POP integral assumes BS
  lognormal terminal distribution. A SABR-corrected POP would be a
  more honest estimate near the tails; deferred until a
  research-validation pipeline exists for it.

---

## 10. Out of scope

- The IV pipeline itself. `iv-ownership-research.md` is its truth
  doc; this doc cross-links where appropriate but does not duplicate.
- The backtesting engine's internal options surface
  (`PythonDataService/app/engine/options/`). Used only by the
  backtest engine, not by any of the surviving routes in §5.
- Non-options trading surfaces (`/data-lab` ex-options-sub-feature,
  `/strategy-lab`, `/lean-engine`, `/edge`, `/portfolio`). Each has
  its own truth doc or roadmap.
- Live-trading infrastructure. This repo is research and validation
  only.

---

## 11. References + PR audit trail

**External references:**

- Hull, J. C. (2017). *Options, Futures, and Other Derivatives*, 9th
  ed. Pearson. (§15.8 BS price, §15.9 worked example, §17.6–17.10
  Greeks, §10.4 parity, §17.13 implied dividend.)
- CBOE VIX White Paper (2019). (For the IV-pipeline cross-validation
  in `iv-ownership-research.md`.)
- Polygon.io API documentation
  ([`docs.polygon.io`](https://polygon.io/docs/options/getting-started)).

**Internal references:**

- [`docs/architecture/options-routes-research.md`](options-routes-research.md)
  — the cleanup plan that produced this doc.
- [`docs/architecture/options-math-authorities.md`](options-math-authorities.md)
  — the canonical implementation index for every formula in §4.
- [`docs/architecture/iv-ownership-research.md`](iv-ownership-research.md)
  — the IV pipeline truth doc that inspired this template.
- [`docs/architecture/options-vol-platform-tdd.md`](options-vol-platform-tdd.md)
  — the migration roadmap that the R8 deferred item will close out.
- [`docs/options-companion-format.md`](../options-companion-format.md)
  — to be absorbed into §5.4.
- [`docs/options-cross-section-overview.md`](../options-cross-section-overview.md)
  — to be absorbed into §4.

**PR audit trail** (this doc):

- *(populated as Phase 5 authoring continues)*

---

## 12. Appendix A — worked numerical examples

### A.1 Hull §15.9 Example 15.6 (BS price + Greeks)

**Inputs:** S=42, K=40, r=0.10, σ=0.20, T=0.5, q=0 (no dividend),
option_type='call'.

**Intermediate values:**

```
d1 = (ln(42/40) + (0.10 + 0.20²/2) · 0.5) / (0.20 · √0.5)
   = (0.04879 + 0.06) / 0.14142
   = 0.7693

d2 = d1 - 0.20 · √0.5 = 0.6278

N(d1) = 0.7791
N(d2) = 0.7349
```

**Outputs:**

```
Call price = 42 · 0.7791 - 40 · e^(-0.10·0.5) · 0.7349
           = 32.722 - 27.962
           = 4.760  (Hull rounds to 4.7594)

Put price  = 40 · e^(-0.10·0.5) · (1 - 0.7349) - 42 · (1 - 0.7791)
           = 10.085 - 9.276
           = 0.809  (Hull rounds to 0.8086)

Delta = 0.7791
Gamma = 0.0497
Theta = -4.301 / year (i.e., -0.0118 / day)
Vega  = 0.0881 per 1% vol move
Rho   = 0.1399 per 1% rate move
```

To reproduce: `python -c "from app.services.bs_greeks import bs_european_price; print(bs_european_price(42, 40, 0.5, 0.10, 0.20, q=0.0, option_type='call'))"`
inside the `polygon-data-service` container.

### A.2 *(further worked examples to be added per §4.x as Phase 5 continues)*

---

## 13. Appendix B — file map

The exhaustive file map for the cleanup (all in-scope code touched by
the consolidation) lives in
[`docs/architecture/options-routes-research.md` § 13 Appendix A](options-routes-research.md#13-appendix-a--file-map-of-every-options-touchpoint).
This appendix lists only the files that *implement* the math and
data flow described in §4–§5.

### B.1 Python (math + service layer)

```
PythonDataService/app/services/bs_greeks.py             # §4.1, §4.2 — BS price + Greeks (canonical)
PythonDataService/app/volatility/solver.py              # §4.3 — IV solver (canonical, with QL fallback)
PythonDataService/app/volatility/analytics.py           # §4.4 — parity-implied forward + dividend
PythonDataService/app/services/strategy_engine.py       # §4.5 — POP, EV, payoff, breakevens
PythonDataService/app/services/quantlib_pricer.py       # §4.6 — multi-engine pricing
PythonDataService/app/routers/options.py                # P1, P2 — REST endpoints
PythonDataService/app/routers/quantlib_options.py       # /quantlib/compare for §5.2
```

### B.2 Backend (.NET / Hot Chocolate v15)

```
Backend/GraphQL/Query.cs                                # G1, G2, G3, G4, G5 resolvers
Backend/Services/Interfaces/IPolygonService.cs          # service contracts
Backend/Services/Implementation/PolygonService.cs       # HTTP proxy to Python
Backend.Tests/Unit/GraphQL/QueryTests.cs                # 23 resolver tests (Phase 2 added 9)
```

### B.3 Frontend (Angular 21)

```
Frontend/src/app/components/strategy-builder/           # §5.1 — survivor (R0b absorbs F1's drill-down)
Frontend/src/app/components/pricing-lab/                # §5.2 — survivor (multi-engine compare)
Frontend/src/app/components/data-lab/                   # §5.3 — survivor (R1 adds past-chain inspector)
Frontend/src/app/shared/payoff-chart/                   # relocated from options-strategy-lab/ in R0a
Frontend/src/app/utils/occ-ticker.ts                    # R5 utility used by §5.3 (and §5.1 after R0b)
Frontend/src/app/utils/black-scholes.ts                 # legacy TS BS — deleted in R8 (deferred)
Frontend/src/app/services/market-data.service.ts        # GraphQL methods for G1–G5
```

---

*End of document.*
