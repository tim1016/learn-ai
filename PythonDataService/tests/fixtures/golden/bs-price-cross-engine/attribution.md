# bs-price-cross-engine — cross-engine parity fixture

**Constructed:** 2026-04-26 (Phase 1.4 of `docs/architecture/numerical-authority-migration-plan.md`)
**Purpose:** pin equivalence between the two in-repo Black-Scholes implementations:
- closed-form, continuous-time → `app/services/bs_greeks.py::bs_european_price`
- QuantLib analytic engine → `app/services/quantlib_pricer.py::price_option` with `engine=PricingEngine.ANALYTIC_BS`

**Why a fixture and not just a parametrized test:** the input cases are pinned here so that both engines must produce a price on the same input grid. If either drifts (a refactor changes a sign convention, a rounding-mode change in QuantLib, etc.), the test detects it via the parity assertion against the *other* engine, not against the fixture's stored output (there is no stored output — both engines compute live).

## Reference

Hull, *Options, Futures, and Other Derivatives* (10e), §15.8 — closed-form Black-Scholes-Merton price.
QuantLib analytic European engine: standard textbook implementation; not an external port reference, just an alternate implementation that should produce identical results for European options under the BSM model.

## Tolerance

Per `.claude/rules/numerical-rigor.md` § Tolerance rules:
- Indicator values default: `atol=1e-9, rtol=0`
- The two BSM implementations both use the same closed-form analytical formula. They must agree to numerical roundoff. Test asserts `atol=1e-10, rtol=0`.

Why this is tighter than the `1e-9` indicator default: there is no recursive accumulation in either path. Single-evaluation closed-form math should be agreement to within last-bit machine precision; if it isn't, something is wrong.

## Input grid

| Dimension | Values | Justification |
|---|---|---|
| `spot` | `[80.0, 100.0, 120.0]` | OTM, ATM, ITM relative to strike=100 |
| `strike` | `[100.0]` | Single strike for clarity; spot moves cover moneyness |
| `ttm_years` | `[7/365, 30/365, 90/365, 180/365, 365/365]` | 1 week to 1 year; avoids QuantLib's TTM=0 collapse |
| `volatility` | `[0.10, 0.20, 0.40]` | 10%, 20%, 40% IV — covers low / typical / high vol regimes |
| `rate` | `[0.0, 0.05]` | Zero rate (clean BS) and typical short rate |
| `dividend` | `[0.0, 0.02]` | Zero and typical equity continuous dividend |
| `option_type` | `[call, put]` | Both wings |

Total cases: `3 × 1 × 5 × 3 × 2 × 2 × 2 = 360`.

## What is *not* covered (and why)

- **TTM < 1 day (intraday / 0DTE):** QuantLib's date-based engine collapses to TTM=0 and returns 0 Greeks. Per `docs/architecture/options-math-authorities.md` § Dispatch rules, the closed-form path is the only correct one for sub-day TTM. A separate test in `tests/services/test_bs_greeks.py::TestSpotCheckFromReconciliation` covers the 0DTE case against a hand-derived reference.
- **American / exotic options:** outside the BSM closed-form domain. Future work; would use QuantLib's binomial or finite-difference engines.

## Regeneration

There is no stored output to regenerate. The fixture defines inputs only; both engines compute live in the test.

If the input grid expands, edit `cases.json` and run the test once to confirm both engines still agree on the new cases.
