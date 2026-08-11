# Partial-fill execution slices

## Source

Synthetic Alpaca `trade_updates` frames authored for PRD #1441, S0.3. They
exercise the adapter's top-level per-execution `qty` and `execution_id`, not
the embedded order's cumulative `filled_qty` / `filled_avg_price`.

## Fixture date

2026-08-10.

## Generation and assumptions

The first NVDA slice is 2 shares at $500.00. The second is a distinct 5-share
slice at $501.00; its enclosing order's `filled_qty=7` is intentionally
cumulative and must never be stored as another seven-share execution. The
effective position is seven shares. With a $501.00 mark, open P&L is
`2 * (501 - 500) + 5 * (501 - 501) = $2.00`.

This is an S1/S2 acceptance oracle. Any change requires a PRD-reviewed
re-derivation from per-execution evidence, not a fixture edit to make a test
pass.

## Tolerance

`atol=1e-6, rtol=0` for P&L; fill-row count, quantities, and execution IDs are
exact.
