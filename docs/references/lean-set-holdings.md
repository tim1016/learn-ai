# Port: LEAN `SetHoldings` share sizing

The sizing law, its LEAN source and the 20-entry golden fixture are in
`PythonDataService/app/engine/execution/sizing.py` and
`PythonDataService/tests/fixtures/golden/lean-set-holdings/attribution.md`.

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
