# Closed-form Black-Scholes Greeks (per-bar) — port attribution

## Target
`PythonDataService/app/services/bs_greeks.py` — `black_scholes_greeks()`,
returning `delta / gamma / theta / vega / rho` for European calls and puts
with continuous dividend yield, taking `ttm_years` directly (no date
arithmetic).

## Reference
Hull, *Options, Futures, and Other Derivatives*, 11th edition,
Chapter 19 ("The Greek Letters"). Standard closed-form Black-Scholes-
Merton Greeks. Cross-references with identical math:

- QuantLib `AnalyticEuropeanEngine` (used at `app/services/quantlib_pricer.py`
  for ttm ≥ 1 day — kept in service for the volatility-surface and
  multi-leg-strategy endpoints where day resolution suffices)
- Wilmott, *Paul Wilmott on Quantitative Finance*, vol. 1, §6 — same
  formulas, alternative derivation
- John C. Cox, "The Constant Elasticity of Variance Option Pricing Model",
  J. Portfolio Management, 1996 (degenerates to standard BS at β=1)

## Why this exists
The data-lab options-companion export had `iv / delta / gamma / theta /
vega` columns 100% NaN despite all checkboxes selected in the UI. Two
issues compounded:

1. The IV solver's `MIN_TIME_TO_EXPIRY = 1.0 / 365.0` (one calendar day)
   short-circuited every 0DTE bar to `SolveStatus.EXPIRED` with
   `iv=None`. With the user running `expiry_mode='same_day'`, every
   single bar was sub-day → 100% null.
2. `quantlib_pricer.price_option()` constructs expiry as
   `ql.Date(expiration_date)` — day resolution — and computes
   `t_years = day_count.yearFraction(eval_date, expiry_date)`. For
   `eval_date == expiry_date`, `t_years = 0`, the function returns
   intrinsic-only with `gamma = theta = vega = rho = 0`. Even with the
   solver guard removed, Greeks would still come out as zero for 0DTE.

See `docs/references/reconciliations/data-lab-spy-2026-04-17-to-2026-04-24.md`
§ Finding 3.1 for the field-observed symptom; the manual spot-check
in that file (`SPY $709 call at 14:00 ET, T = 2/(365×24)` →
`IV ≈ 0.1789`, `Δ ≈ 0.7203`) is the anchor used as a regression test.

## Math summary
Standard BSM with continuous dividend yield `q`:

```
d1 = [ln(S/K) + (r − q + σ²/2)·T] / (σ·√T)
d2 = d1 − σ·√T

Δ_call =  e^(−qT) · N(d1)
Δ_put  =  e^(−qT) · (N(d1) − 1)

Γ      =  e^(−qT) · φ(d1) / (S · σ · √T)

Θ_call = −S·e^(−qT)·φ(d1)·σ / (2·√T)
         − r·K·e^(−rT)·N(d2)
         + q·S·e^(−qT)·N(d1)
Θ_put  = −S·e^(−qT)·φ(d1)·σ / (2·√T)
         + r·K·e^(−rT)·N(−d2)
         − q·S·e^(−qT)·N(−d1)

Vega   =  S · e^(−qT) · φ(d1) · √T
ρ_call =  K · T · e^(−rT) · N(d2)
ρ_put  = −K · T · e^(−rT) · N(−d2)
```

## Conventions (match `quantlib_pricer.GreeksResult`)
- `theta` returned **per calendar day** (annual / 365)
- `vega`  returned **per 1% IV move** (raw / 100)
- `rho`   returned **per 1% rate move** (raw / 100)

## Equivalence level
**Strict-float** (Hull's formulas implemented symbolically; deterministic
across runs). Cross-checks:

- **Put-call parity in Greeks**: `Δ_call − Δ_put = e^(−qT)`,
  `Γ_call = Γ_put`, `Vega_call = Vega_put` — enforced to `1e-12`.
- **Round-trip via the IV solver**: BS theoretical price → solver IV
  → recovered IV vs input σ within `1e-3` (QuantLib path is bounded by
  serial-day rounding; sub-day Brent path is tighter).
- **Spot-check vs the SPY 0DTE reconciliation**: `Δ ≈ 0.7203` for
  `S=710.109, K=709, T=2/(365·24), σ=0.1789, r=0.05, q=0` —
  enforced to `1e-3`.

## Tolerances
- Symbolic formulas: deterministic, bit-exact within numpy `float64`.
- IV round-trip: `atol=1e-3`. Looser than the underlying Brent
  convergence (1e-10) because QuantLib's day-resolution serial-date
  arithmetic on the primary path introduces ≤0.5/365 yr rounding error
  in `T`, which propagates to `IV`. Sub-day TTM bypasses QuantLib
  entirely (see `solver.py` § "QuantLib solve" comment) and recovers
  to Brent precision.
- Spot-check anchor: `atol=1e-3` — same QL-rounding budget; the
  reconciliation report's `0.7203` is itself rounded to four places.

## Tests
- `PythonDataService/tests/services/test_bs_greeks.py`
  - `TestSpotCheckFromReconciliation::test_call_delta_matches_reconciliation`
  - `TestPutCallParity::*` (delta diff, gamma equality, vega equality)
  - `TestSelfConsistency::test_iv_round_trip` (5 parameterized cases)
  - `TestSubDayTtm::test_two_hour_ttm_resolves`,
    `TestSubDayTtm::test_thirty_minute_ttm_does_not_return_expired`
  - `TestInputGuards::test_non_positive_input_raises` (4 cases)
- `PythonDataService/tests/services/test_options_companion_service.py`
  - `TestBuildOptionsCompanionTimestampAlignment::test_pre_rth_option_bars_dropped_and_greeks_populated`
  - `TestBuildOptionsCompanionTimestampAlignment::test_warning_emitted_when_greek_column_is_100pct_empty`
- `PythonDataService/tests/volatility/test_solver.py`
  - `TestImpliedVolatility::test_expired_option_returns_expired` —
    pinned to the new 1-minute floor.

## Sovereignty
No runtime dependency on QuantLib for sub-day TTM. `quantlib_pricer`
remains in service for ≥ 1-day endpoints where day resolution is
sufficient (volatility surface, multi-leg strategy pricing).
