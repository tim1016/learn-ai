# portfolio-scenario-3leg — golden fixture

**Constructed:** 2026-04-26 (Phase 2.1 of `docs/architecture/numerical-authority-migration-plan.md`)
**Purpose:** Pin the integration behavior of `evaluate_scenario` (in `app/services/portfolio_scenario.py`) against direct Hull-formula reference computation.

## Fixture composition

A 3-leg covered-call-with-put-protection on SPY:

| Leg | Side | Type | Strike | Quantity | Entry premium | Notes |
|---|---|---|---|---|---|---|
| 1 | long | stock | n/a | 100 shares | $610.00 | the underlying |
| 2 | short | call | $620 | -1 contract | $4.50 | OTM call sold for premium |
| 3 | long | put | $600 | +1 contract | $5.20 | OTM put bought for downside protection |

Underlying: SPY @ spot 615.00, 30 days to expiration, IV 22%, r=4.3%.

## Grid

5×5 (spot, time) grid with iv_shift=0:
- spot shocks: `[-0.05, -0.02, 0.0, +0.02, +0.05]`
- time shifts (calendar days): `[0.0, 5.0, 15.0, 20.0, 28.0]`
- iv shifts: `[0.0]`

25 scenario points total.

## Reference

Hull, *Options, Futures, and Other Derivatives* (10e), §15.8 (BS price), §19 (Greeks). The reference is the *closed-form formula itself*, computed independently in the test using the same `bs_greeks.py` primitives — so this is a wiring test, not a math-validity test (the math is validated by `tests/services/test_bs_greeks.py` and `tests/services/test_bs_cross_engine_parity.py`).

What the test catches:
- Quantity / multiplier wiring bugs (e.g., scaling a stock by 100 or an option by 1)
- Sign convention flips (long/short in the aggregation)
- Per-leg ↔ aggregate inconsistency (sum-of-legs ≠ aggregate)
- Time-shift bug (TTM not advancing correctly with `time_shift_days`)
- Spot-shock bug (effective_spot not equal to `spot * (1+shock)`)

## Tolerance

`atol=1e-6, rtol=1e-6` per `.claude/rules/numerical-rigor.md` Greeks default. Per-share BS values agree at machine precision; aggregate values can accumulate up to ~25 leg-evaluations × 100 multiplier = 2500x scale, so 1e-6 absolute is comfortable.

## Regeneration

Not applicable — the test computes both the system-under-test (`evaluate_scenario`) and the reference (direct Hull-formula calls) at runtime. There is no stored output to regenerate. If the leg specification changes, edit `cases.json` and the test will adapt.
