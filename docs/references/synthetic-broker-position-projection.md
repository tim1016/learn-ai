# Synthetic broker position projection

The isolated Dry Run broker projects positions from its own durable simulated
fills. It is not an Alpaca account value, a substitute market mark, or a
second source of real custody truth.

## Formula

For each symbol, the projection folds filled orders in durable sequence order.
It carries `(quantity, signed_entry_notional)`:

- A same-side fill adds its signed quantity and `signed quantity × fill price`.
- A reducing fill retains the old average entry price for the remaining
  quantity; sale proceeds are realized P&L, not new entry cost.
- A fill that crosses flat opens only its residual at that fill's price.
- The result is emitted only when the shared Clerk predicate
  `position_quantity_is_nonzero(quantity)` holds (`abs(quantity) >= 1e-9`).

`average_entry_price = abs(signed_entry_notional / quantity)` and both
`market_value` and `cost_basis` are `abs(signed_entry_notional)`. The adapter
does not author mark-to-market price or P&L; those fields remain unavailable or
zero in the broker contract.

## Authority and proof

`PythonDataService/app/broker/alpaca/clerk/synthetic_broker.py::_project_positions`
is the sole implementation. It consumes only the synthetic authority's
append-only order ledger and exact retained-bar fill receipts.

`PythonDataService/tests/services/test_source_bar_ledger.py::test_synthetic_position_projection_preserves_average_cost_through_reduce_and_flip`
pins buy, partial reduction, add, and side-flip behavior exactly (`atol=0`,
`rtol=0`).
