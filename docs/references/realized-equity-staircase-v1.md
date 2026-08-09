# Realized-equity staircase v1

## Canonical contract

`PythonDataService/app/engine/results/equity_downsample.py::build_realized_equity_envelope`
authors the persisted closed-trade equity staircase. It starts with persisted
initial cash and adds each persisted net `BacktestTrade.PnL` at the completed
trade's `exit_ms_utc`:

`E(t) = initial_cash + Σ(net_pnl_i where exit_i <= t)`.

This is distinct from the independently authored mark-to-market curve. It is
evidence for realized P&L, not an input to Sharpe, Sortino, or drawdown.

## Timestamp and boundary rules

- All inputs and output timestamps are `int64 ms UTC`.
- Exits sort by `(exit_ms_utc, trade_number)`; equal timestamps aggregate into
  one post-exit point.
- An exit exactly at the start or end boundary owns that boundary timestamp;
  no duplicate pre- or post-anchor is emitted.
- Exits outside the producer-authored covered bounds are rejected.

## Validation receipt

`PythonDataService/tests/fixtures/golden/ENG-006/` contains synthetic ledger
inputs and an independently hand-computed Decimal oracle. The fixture checks
equal-exit aggregation and both boundary-folding cases without importing the
canonical implementation. `test_realized_equity_matches_golden_fixture` pins
`atol=1e-6, rtol=0`, the fixed transport tolerance for accumulated P&L.

Regenerate the frozen receipt with:

```bash
python3 PythonDataService/scripts/fixture_generators/realized_equity_staircase.py
```
