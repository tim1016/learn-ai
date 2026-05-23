# Port: LEAN `SetHoldings` share sizing

## What was ported

LEAN's `QCAlgorithm.SetHoldings(symbol, target)` share-quantity computation
for the **long-only equity** case — ported as the `lean_set_holdings`
sizing model in `PythonDataService/app/engine/execution/sizing.py`
(`LeanSetHoldingsSizing`).

This is a deliberately **narrow** port. It covers the
`SetHoldings(symbol, target)` path the cross-engine parity matrix
exercises, NOT LEAN's full buying-power universe (margin/cash accounts,
leverage, shorts, multi-currency cash books, option buying-power models,
open-order reservations, minimum-order thresholds, iterative
multi-asset fee models). Those are out of scope until a use case needs
them; porting them now would be bloat.

## Source

- **Construct**: LEAN `QCAlgorithm.SetHoldings` →
  `IBuyingPowerModel.GetMaximumOrderQuantityForTargetBuyingPower`.
- **Pinned via**: the LEAN container image digest
  `sha256:97884667be20077925996ac22b5e3e16e3a47e7363e01795151459d16786247c`
  run against the EMA-crossover trusted sample
  (`app/lean_sidecar/trusted_samples/ema_crossover.py`).
- LEAN source for the buying-power model is not vendored in `references/`;
  the port is pinned instead to LEAN's *observed output* on a fixed
  image digest (the golden fixture below), which is sovereign and
  reproducible.

## The math

```
qty = floor( (min(target_value, buying_power) - order_fee) / price )
  target_value = portfolio_value * target_fraction
  buying_power = portfolio_value * (1 - FreePortfolioValuePercentage)
```

`FreePortfolioValuePercentage = 0.0025` — LEAN's documented default
free-portfolio-value buffer (the slice LEAN holds back so an order is not
rejected for insufficient buying power). `order_fee` is the per-order
commission the run charges; the Engine wires it from the fill model so the
reservation matches what the run will actually be charged.

For `target_fraction = 1.0` this reduces to
`floor((portfolio_value * 0.9975 - order_fee) / price)`.

## Golden fixture & tolerance

- **Fixture**: `PythonDataService/tests/fixtures/golden/lean-set-holdings/`
  — `entries.json` is every from-flat `SetHoldings(SPY, 1.0)` entry (20 of
  them) from the pinned LEAN run for cell
  `SPY_W6mo_2025-11-03_to_2026-04-30`, with the portfolio value, price,
  per-order fee, and the share count LEAN chose.
- **Test**: `PythonDataService/tests/engine/test_sizing.py`.
- **Tolerance**: `atol=0` — exact integer share-count reproduction. All 20
  entries match.

## Why this matters

The previous Engine sizing (`SimpleFloorSizing`,
`floor(portfolio_value * fraction / price)`, no buffer) bought **one share
more** than LEAN on every trade — the `QUANTITY_MISMATCH` divergence the
cross-engine parity matrix Gate 3 surfaced. `SimpleFloorSizing` is kept as
an explicit research/legacy policy; it is **not** LEAN parity. LEAN-pinned
runs (`cross_runner.run_engine_lab_on_workspace`) and the golden matrix use
`lean_set_holdings`, pinned in each cell's `manifest.json`
`runtime_parameters.sizing_model`, and Gate 3 holds `qty_atol = 0`.

## Known divergences

None. Exact reproduction across the 20-entry fixture.
