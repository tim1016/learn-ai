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

## Fee-aware sizing (IBKR brokerage, optional path)

When the LEAN side runs under `SetBrokerageModel(InteractiveBrokersBrokerage,
AccountType.Margin)`, `SetHoldings` is sized against an implicit equation
because the per-fill IBKR fee depends on the share count itself:

```
qty = max integer such that  qty*price + fee(qty, price) <= cap
       cap = portfolio_value * (1 - FreePortfolioValuePercentage)
       fee = IbkrEquityCommissionModel.fee(qty, price)
```

The Python port adds this as an opt-in branch on the same class. When the
optional `fee_model: IbkrEquityCommissionModel | None = None` field is set,
`LeanSetHoldingsSizing.target_quantity(...)` ignores the caller-supplied
`order_fee` argument and computes the fee per iteration via the injected
model, decrementing `qty` from the naive `int(cap / price)` floor until the
equation holds. The IBKR per-share rate (`$0.005`) is small relative to
realistic equity prices, so the loop typically converges in one iteration
— the floor is the answer when the $1 min binds.

When `fee_model is None`, the legacy fixed-`order_fee` path is byte-identical
to today's behavior (the 20-entry SPY golden fixture is the regression gate).

The cross-engine matrix runs wire this path via
`PythonDataService/app/lean_sidecar/cross_runner.py` by passing
`LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel())` to
`BacktestEngine(sizing_model=...)` and the same fee model to
`FillModel(fee_model=...)`. SPY_W6mo is the first cell pinned under this
contract; QQQ/AAPL/TSLA pinning is deferred behind a separate engine-side
fill-mode fix (see the matrix README and design spec for the cross-session
exit-fill-mode gap).

## Known divergences

None. Exact reproduction across the 20-entry fixture (legacy path) and
the fee-aware path's SPY parity case.

## 2026-06-13 — Live-path cutover (PR2 of ADR 0009)

`LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel())` is now the
*live* `SetHoldings` resolver too — wired through
`app/engine/execution/order_sizer.py::OrderSizer` and called from
`LivePortfolio.set_holdings` when the live `live_config.sizing.kind ==
"SetHoldings"`. `SimpleFloorSizing` stays as a `LivePortfolio.sizing_model`
default for replay paths that never attach an `OrderSizer`, but a
sizing-aware live deploy never touches `SimpleFloorSizing` again.

**Intentional behavior change**: every live `SetHoldings` run will now buy
**fewer shares** than the previous live default (1–2 shares fewer per
entry, depending on price). The shift is the same one the cross-engine
parity matrix already documents — see "Why this matters" above. It is the
*honest* LEAN-native quantity; the prior 1-share-extra was the
`SimpleFloorSizing` bug Gate 3 surfaced. The regression test in
`tests/engine/execution/test_order_sizer.py` pins the new live-path
output to `LeanSetHoldingsSizing`'s share count and explicitly contrasts
it with `SimpleFloorSizing` so any future drift surfaces immediately.

No new fixture is required — `LeanSetHoldingsSizing` is the canonical
quantity-math authority (pinned at `atol=0` by the 20-entry SPY golden);
this cutover only changes *which path the live engine takes through it*.
